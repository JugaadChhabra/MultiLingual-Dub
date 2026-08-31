/* Video pane — the video editor's screen.
   An avatar and a script in, rendered video out, filed to the NAS.

   Three modes, in the order they cost the operator work: Auto writes the day's
   scripts from the category's brief and shows them to be read and edited, Single
   takes one typed script, Batch takes a spreadsheet of them. Auto and Batch end
   up at the same endpoint — reviewed drafts and spreadsheet rows are the same
   thing by the time they are submitted. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  // Shipped preset. localStorage holds only the operator's changes on top of
  // this, so editing here ships new copy immediately unless they edited it too.
  const SEED = [{
    id: "sunsign", label: "Horoscope",
    // Written off the hand-authored reference sheet, so a generated day is
    // indistinguishable in shape from the days the editor wrote themselves.
    // The fortune line's FORMAT is fixed there too; its values are not, and
    // neither is the health line \u2014 a sentence identical across twelve signs
    // every day is the one duplicate the uniqueness rules used to step around.
    //
    // Areas are drawn from a per-sign pool rather than fixed, so ten days do not
    // produce ten variations on one script. Every pool carries at least six:
    // five would give exactly C(5,3)=10 combinations, which is zero slack over a
    // ten-day window and impossible on day eleven.
    script_brief: "A daily horoscope for one zodiac sign, for an Indian audience, voiced by a calm and authoritative astrologer.\n\nEach sign has a pool of life areas. Choose THREE from that sign's pool for today, and choose a combination this sign has not used on a recent day. Vary which three, and the order you name them in.\n\nमेष — नई शुरुआत, ऊर्जा, नेतृत्व, साहस, प्रतिस्पर्धा, आत्मविश्वास\nवृषभ — धन, रिश्ते, सुख-सुविधा, स्थिरता, भोजन, धैर्य\nमिथुन — संवाद, यात्रा, सामाजिक जीवन, सीखना, मित्रता, विचार\nकर्क — परिवार, घर, भावनात्मक जीवन, स्मृतियाँ, देखभाल, अंतर्ज्ञान\nसिंह — करियर, सम्मान, नेतृत्व, रचनात्मकता, आत्मप्रदर्शन, गर्व\nकन्या — स्वास्थ्य, कार्यक्षेत्र, योजना, अनुशासन, सेवा, विवरण\nतुला — रिश्ते, साझेदारी, संतुलन, न्याय, सौंदर्य, सामाजिक मेलजोल\nवृश्चिक — आंतरिक शक्ति, गोपनीयता, परिवर्तन, गहराई, जुनून, रहस्य\nधनु — यात्रा, ज्ञान, विस्तार, स्वतंत्रता, आशावाद, दर्शन\nमकर — करियर, अनुशासन, जिम्मेदारी, महत्वाकांक्षा, धैर्य, संरचना\nकुंभ — मित्रता, नवाचार, सामाजिक कार्य, स्वतंत्र सोच, समुदाय, विद्रोह\nमीन — आध्यात्म, कल्पनाशीलता, भावनाएं, करुणा, सपने, अंतर्ज्ञान\n\nHook paragraph: address the sign's natives and then name today's three areas, as a bare list with no connecting clause, ending in an exclamation — exactly this shape: \"<राशि> राशि के जातकों... आज नई शुरुआत, ऊर्जा और नेतृत्व!\"\n\nBody paragraph: one concrete, relatable prediction per area, in the order named. Keep them broad and human — no job titles, no industries, no technical specifics. Drive each prediction with a different narrative angle, rotating across these five and never using the same angle twice in one script: an opportunity arriving (\"एक अवसर मिलेगा...\"), a tangle resolving (\"एक उलझन सुलझेगी...\"), an unexpected turn (\"अप्रत्याशित रूप से...\"), support from someone close (\"किसी अपने का सहयोग मिलेगा...\"), an inner shift (\"आपके भीतर स्पष्टता आएगी...\").\n\nThen one line on health and mental wellbeing, phrased fresh every time \u2014 your own words, never a sentence you have used for another sign today or on a recent day. Then close the paragraph with, in this exact format: \"शुभ रंग: <a vivid specific colour, e.g. गहरा रूबी रेड> | जादुई अंक: <a number from 10 to 99 in words, with the Devanagari digits in brackets, e.g. सत्रह (१७)>।\"\n\nThe lucky number is always double-digit — never a single digit, never three digits.\n\nClosing paragraph: one line of direct encouragement to this sign, matching what was predicted.\n\nThe three areas, the colour and the number must differ across all twelve signs and must not repeat what recent days used. A different shade of a colour already used still counts as that colour.",
    // motion_prompt drives BODY MOTION and HAND GESTURES only — not facial
    // expression, lip-sync or eyes (the engine handles those from the audio),
    // and not energy level (that is the separate `expressiveness` control). So
    // this describes posture, head and hands and nothing the parameter ignores.
    motion_prompt: "The speaker faces the camera directly with a calm, grounded presence, holding an upright and mostly still posture with relaxed shoulders and only subtle, natural weight shifts — no walking or large body movements. The head moves gently in time with the speech: slow, affirming nods and occasional soft side-to-side tilts. The hands make slow, deliberate open-palm gestures near the chest — opening outward to reassure, then settling back to rest — unhurried, smooth, and never repetitive or fidgety. The overall motion is serene and warm, conveying quiet authority and compassion.",
  }];
  const SEED_IDS = new Set(SEED.map((c) => c.id));
  // A retired id is no longer in SEED_IDS, so without this the stored `user`
  // array would resurrect it as an operator-created category.
  const RETIRED = new Set(["birthday"]);
  const FIELDS = ["label", "motion_prompt", "script_brief"];
  const STORE = "autodub_video_cat_store_v1", ACTIVE = "autodub_video_active_cat";
  const CHAR = "autodub_video_character", JOB_KEY = "autodub_video_job";
  const MODE = "autodub_video_mode", LANG = "autodub_video_script_lang";
  const SET = "autodub_video_script_set", DRAFTS = "autodub_video_drafts";
  // Drafts older than this are not worth restoring — a horoscope for last week
  // is not a draft, it is history.
  const DRAFT_KEEP_DAYS = 7;
  // Matches MAX_SCRIPT_CHARS in services/script_writer.py and the maxlength on
  // the single-mode textarea: past this HeyGen will not render the script.
  const MAX_SCRIPT = 1000;
  // Which languages get the native face in the draft editor. Latin-script
  // targets read worse in it than in the UI font.
  const NATIVE_LANGS = new Set(["bn-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN",
                                "od-IN", "pa-IN", "ta-IN", "te-IN", "ru"]);

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
      } else user.push({ id: c.id, ...Object.fromEntries(FIELDS.map((k) => [k, c[k]])) });
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
    auto: $("#vAuto"), lang: $("#vLang"), gen: $("#vGen"), genSub: $("#vGenSub"), drafts: $("#vDrafts"),
    set: $("#vSet"), autoTitleRow: $("#vAutoTitleRow"), autoTitle: $("#vAutoTitle"),
    inner: document.querySelector("#pane-video .pane-inner"),
    work: $("#vWork"), draftsWrap: $("#vDraftsWrap"), queueWrap: $("#vQueueWrap"),
    draftsN: $("#vDraftsN"),
    title: $("#vTitle"), drop: $("#vDrop"), xls: $("#vXls"),
    dropTitle: $("#vDropTitle"), dropSub: $("#vDropSub"), queue: $("#vQueue"),
    progress: $("#videoProgress"), pCount: $("#vpCount"), pDetail: $("#vpDetail"),
    up: $("#vpUp"), done: $("#vpDone"), bad: $("#vpBad"),
    upN: $("#vpUpN"), doneN: $("#vpDoneN"), badN: $("#vpBadN"), elapsed: $("#vpElapsed"),
    run: $("#vRun"), cancel: $("#vCancel"), msg: $("#vMsg"), live: $("#videoLive"),
  };

  const state = { image: null, photoId: null, stillURL: null, excel: null, excelProblem: null,
                  mode: "auto", character: localStorage.getItem(CHAR) || "indian",
                  // Written scripts the operator is reviewing. Held here and not
                  // on the server: nothing is committed until Run.
                  drafts: [], writing: false, set: "zodiac",
                  running: false, jobId: null, kind: null, startedAt: null };
  // Auto and Batch both submit rows, so everything about the run — the endpoint,
  // the progress rendering, the reattach — keys off this, not off the mode.
  const submitsRows = () => state.mode !== "single";

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
    els.monitor.dataset.empty = "1";
    els.body.innerHTML = `<div class="ph"><div class="t">No avatar yet</div>
      <div class="s">Drag an image here, or pick one</div>
      <div class="row"><button type="button" class="btn" id="vBrowse">Upload image</button>
      <button type="button" class="btn" id="vReuse">Reuse an avatar</button></div></div>`;
    $("#vBrowse").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) els.img.click(); });
    $("#vReuse").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) openAvatars(); });
    els.tag.textContent = "No input"; els.file.hidden = true;
    delete els.body.dataset.src;
  }
  function showStill(tag) {
    els.monitor.dataset.empty = state.stillURL ? "0" : "1";
    els.body.innerHTML = state.stillURL ? `<img src="${state.stillURL}" alt="Selected reference avatar" />` : "";
    els.tag.textContent = tag || "Reference";
    els.file.hidden = !(state.image || state.photoId);
    delete els.body.dataset.src;
  }
  // Clips this machine has already failed to read. The dataset guard below only
  // holds while the clip is mounted, and giving up unmounts it — so without
  // this, a genuinely broken file is retried on every poll tick forever: mount,
  // fail, fall back to the still, remount four seconds later.
  const unplayable = new Set();
  function showVideo(url) {
    // The batch poller reports every completed row every four seconds, so this
    // is called repeatedly with a clip that is already on screen. Rebuilding
    // the element each time restarted playback under the operator and armed a
    // fresh unplayable-clip timer, which is what was stacking notices.
    if (unplayable.has(url) || els.body.dataset.src === url) return;
    els.body.dataset.src = url;
    els.monitor.dataset.empty = "0";
    els.body.innerHTML = `<video src="${url}" controls autoplay playsinline></video>`;
    els.tag.textContent = "Playback"; els.file.hidden = true;
    const v = els.body.querySelector("video");
    if (!v) return;
    // A truncated download does not reliably fire `error` — Chrome sits at
    // networkState=LOADING forever — so give up on a metadata timeout too.
    let settled = false;
    const giveUp = (why) => { if (settled) return; settled = true; clearTimeout(t);
      unplayable.add(url);
      showStill("Clip unavailable");
      U.toast("The clip could not be played", { kind: "warn", id: "clip:" + url,
        detail: `It rendered, but this machine could not read the file (${U.esc(why)}).` }); };
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
    refreshRunLabel();
    if (mode === "auto") refreshGen();
    refreshRun();
  }
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

  // ── auto: written scripts ────────────────────────────────────────────────
  // Writing and rendering are two requests on purpose. A render is paid for the
  // moment it is submitted, so the operator reads twelve drafts — and fixes the
  // ones that need it — before any of that money is spent.
  try {
    const savedLang = localStorage.getItem(LANG);
    if (savedLang && els.lang.querySelector(`option[value="${savedLang}"]`)) els.lang.value = savedLang;
  } catch (_) {}
  els.lang.addEventListener("change", () => {
    try { localStorage.setItem(LANG, els.lang.value); } catch (_) {}
    saveDrafts();
    // The drafts on screen are in the previous language; keep them but let the
    // editor re-decide the face they are typeset in.
    renderDrafts();
  });

  U.bindSeg(els.set, ({ set }) => applySet(set));
  function applySet(set, persistIt = true) {
    state.set = set;
    if (persistIt) { try { localStorage.setItem(SET, set); } catch (_) {} }
    els.set.querySelectorAll("button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.set === set)));
    els.autoTitleRow.hidden = set !== "single";
    refreshGen(); refreshRun();
  }
  els.autoTitle.addEventListener("input", refreshRun);

  // The button names its consequence: twelve paid renders is worth reading before
  // pressing, and "Generate batch" never said how many.
  function refreshRunLabel() {
    if (state.mode === "single") { els.run.textContent = "Generate video"; return; }
    const n = state.mode === "auto" ? state.drafts.length : 0;
    els.run.textContent = n ? `Render ${n} video${n === 1 ? "" : "s"}` : "Generate batch";
  }

  function refreshGen() {
    const c = activeCat();
    const brief = c && (c.script_brief || "").trim();
    const zodiac = state.set === "zodiac";
    els.gen.disabled = state.writing || state.running || !brief
      || (!zodiac && !els.autoTitle.value.trim());
    els.gen.textContent = state.writing ? "Writing…" : state.drafts.length ? "Rewrite scripts" : "Write scripts";
    els.genSub.textContent = !c ? "Pick a category first"
      : !brief ? `“${c.label}” has no script brief — add one in the category editor`
      : !zodiac && !els.autoTitle.value.trim() ? "Name the file first"
      : zodiac ? "Twelve signs, from this category's brief"
      : "One script, from this category's brief";
  }

  // Drafts survive a reload. They are the operator's unsaved work — twelve
  // scripts they may have spent ten minutes editing — and losing them to a
  // refresh cost a full Gemini run and every edit made since.
  const today = () => new Date().toISOString().slice(0, 10);
  function saveDrafts() {
    try {
      if (!state.drafts.length) { localStorage.removeItem(DRAFTS); return; }
      localStorage.setItem(DRAFTS, JSON.stringify({
        publish_date: els.date.value || today(),
        language: els.lang.value, category: (activeCat() || {}).id || "",
        set: state.set, title: els.autoTitle.value,
        items: state.drafts.map((d) => ({ video_title: d.video_title, script: d.script, edited: !!d.edited })),
      }));
    } catch (_) {}
  }
  function restoreDrafts() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(DRAFTS)); } catch (_) {}
    if (!saved || !Array.isArray(saved.items) || !saved.items.length) return;

    const age = (Date.parse(today()) - Date.parse(saved.publish_date || "")) / 86400000;
    if (!(age >= 0) || age > DRAFT_KEEP_DAYS) { try { localStorage.removeItem(DRAFTS); } catch (_) {} return; }

    state.drafts = saved.items.map((item) => ({
      video_title: String(item.video_title || ""), script: String(item.script || ""),
      edited: !!item.edited,
    }));
    // The language and set decide how the drafts are typeset and validated, and
    // the date decides where a run of them is filed — restore all three, or the
    // restored drafts would be run under settings they were not written for.
    if (saved.language && els.lang.querySelector(`option[value="${saved.language}"]`)) {
      els.lang.value = saved.language;
    }
    if (saved.title) els.autoTitle.value = saved.title;
    if (saved.set === "single" || saved.set === "zodiac") applySet(saved.set, false);
    if (saved.category && cats.some((c) => c.id === saved.category)) {
      activeId = saved.category; persist(); renderCats();
    }
    if (saved.publish_date) els.date.value = saved.publish_date;

    if (saved.publish_date !== today()) {
      U.banner(els.banners, { kind: "info", title: `Restored ${state.drafts.length} drafts from an earlier day`,
        detail: `They were written for <code>${U.esc(saved.publish_date)}</code>, and the NAS folder date has been set to match. Rewrite them if you meant today.` });
    }
  }

  els.gen.addEventListener("click", write);
  async function write() {
    const c = activeCat();
    if (!c || !(c.script_brief || "").trim() || state.writing || state.running) return;
    if (state.set === "single" && !els.autoTitle.value.trim()) return;
    if (!dateSet()) return;   // scripts are filed by date; no silent default
    if (state.drafts.length) {
      const edited = state.drafts.some((d) => d.edited);
      if (edited) {
        U.confirm({ title: "Replace the drafts you edited?",
          message: "Rewriting throws away every script on screen, including your edits.",
          confirmLabel: "Rewrite", onOk: () => { state.drafts = []; write(); } });
        return;
      }
    }

    state.writing = true; refreshGen(); say("Writing today's scripts…");
    U.clearBanners(els.banners);
    const fd = new FormData();
    fd.append("brief", c.script_brief.trim());
    fd.append("language", els.lang.value);
    fd.append("category", c.id);
    fd.append("item_set", state.set);
    if (state.set === "single") fd.append("title", els.autoTitle.value.trim());
    // The date the run is filed under is also the date the scripts are FOR, and
    // the day whose history is skipped when rewriting. Guaranteed set above.
    fd.append("publish_date", els.date.value);

    try {
      // A dozen scripts in one Gemini call; slower than a normal request, so the
      // timeout is generous rather than the shared default.
      const r = await U.fetchT("/video/scripts/generate", { method: "POST", body: fd }, 180000);
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
      state.drafts = (body.items || []).map((item) => ({
        video_title: item.video_title || "", script: item.script || "", edited: false,
      }));
      saveDrafts();
      say(`${state.drafts.length} scripts written — read them before you run`);
    } catch (e) {
      U.banner(els.banners, { kind: "error", title: "The scripts were not written",
        detail: "Nothing was rendered and nothing was charged.", raw: String(e && e.message || e) });
      say("");
    } finally {
      state.writing = false; renderDrafts(); refreshGen(); refreshRun();
    }
  }

  // A title becomes the NAS filename, so the same scrubbing the single-mode
  // title gets applies here — and it is applied to what is STORED, so what the
  // operator reads is what gets filed.
  const scrubTitle = (v) => v.trim().replace(/[\\/:*?"<>|]+/g, "_").replace(/\.mp4$/i, "");

  // A textarea in a display:none subtree reports scrollHeight 0, so the boxes
  // sized at boot came out at their minimum — the pane is hidden until the shell
  // opens the Video section. The shell calls this when it does, and a resize
  // rewraps the text, which changes the height it needs.
  function fitAll() {
    els.drafts.querySelectorAll("textarea").forEach((a) => {
      a.style.height = "auto"; a.style.height = a.scrollHeight + "px";
    });
  }
  window.addEventListener("resize", fitAll);

  function renderDrafts() {
    els.draftsN.textContent = state.drafts.length || "";
    if (!state.drafts.length) {
      els.drafts.innerHTML = `<div class="empty"><div class="g" aria-hidden="true">✎</div>
        <div class="t">No drafts yet</div>
        <div class="s">Written scripts appear here to read and edit before anything renders</div></div>`;
      return;
    }
    const nat = NATIVE_LANGS.has(els.lang.value) ? " nat" : "";
    els.drafts.innerHTML = "";
    state.drafts.forEach((draft, i) => {
      const el = document.createElement("div");
      el.className = "draft";
      el.innerHTML = `<div class="hd">
          <input type="text" class="fld" maxlength="80" value="${U.esc(draft.video_title)}"
            aria-label="File name for draft ${i + 1}" />
          <span class="counter"></span>
        </div>
        <textarea class="area${nat}" maxlength="${MAX_SCRIPT}"
          aria-label="Script for draft ${i + 1}">${U.esc(draft.script)}</textarea>`;

      const title = el.querySelector("input"), area = el.querySelector("textarea");
      const counter = el.querySelector(".counter");
      // Grown to fit its own text rather than given a fixed height: a review box
      // that clips the last paragraph is a box whose last paragraph nobody reads,
      // and script lengths differ by sign. The CSS min-height is the floor.
      const fit = () => { area.style.height = "auto"; area.style.height = area.scrollHeight + "px"; };
      const recount = () => {
        const n = area.value.length;
        counter.textContent = `${n} / ${MAX_SCRIPT}`;
        // Silent while the length is unremarkable. A count shown twelve times
        // over is noise; a count shown when a script is empty or nearly too long
        // is information.
        el.dataset.flag = !area.value.trim() ? "bad" : n > MAX_SCRIPT - 120 ? "near" : "";
      };
      title.addEventListener("input", () => {
        draft.video_title = scrubTitle(title.value); draft.edited = true; saveDrafts(); refreshRun();
      });
      area.addEventListener("input", () => {
        draft.script = area.value; draft.edited = true; recount(); fit(); saveDrafts(); refreshRun();
      });
      recount();
      els.drafts.appendChild(el);
      fit();   // after insertion — scrollHeight is 0 while the node is detached
    });
  }

  // ── validity ─────────────────────────────────────────────────────────────
  function blocker() {
    if (!cats.length) return "Create a prompt category to start";
    if (!activeCat()) return "Pick a category";
    if (state.mode === "auto") {
      // the batch endpoint takes an image upload only — no talking_photo_id
      if (!state.image) return "Upload an avatar — a set needs its own image, not a reused one";
      if (state.writing) return "Waiting for the scripts";
      if (!state.drafts.length) return "Write today's scripts first";
      if (state.drafts.some((d) => !d.script.trim())) return "One of the drafts is empty";
      if (state.drafts.some((d) => !d.video_title.trim())) return "Every draft needs a file name";
      const titles = state.drafts.map((d) => d.video_title.trim().toLowerCase());
      if (new Set(titles).size !== titles.length) return "Two drafts share a file name — one would overwrite the other";
    } else if (state.mode === "batch") {
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
    if (state.mode === "auto") { refreshGen(); refreshRunLabel(); }
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
    if (!dateSet()) return;   // the date prints on the card and files the render
    say(""); U.clearBanners(els.banners);
    U.askNotify();   // not awaited — see pane-audio.js

    const c = activeCat();
    state.running = true; state.kind = submitsRows() ? "batch" : "single";
    state.startedAt = new Date().toISOString();
    els.live.hidden = false; refreshRun();
    window.Shell.setRunning("video", true, "Video rendering");
    els.tag.textContent = "Rendering"; els.progress.hidden = false;

    const fd = new FormData();
    if (state.image) fd.append("image", state.image);
    else fd.append("talking_photo_id", state.photoId);
    fd.append("character", state.character);
    // The date names the NAS folder, prints on the card, and in Auto is the
    // history key. Required at submit (dateSet), so no fallback here.
    fd.append("publish_date", els.date.value);
    if (c.motion_prompt) fd.append("motion_prompt", c.motion_prompt);

    try {
      let url, id;
      if (submitsRows()) {
        // One endpoint, two ways in: a spreadsheet, or the drafts as they were
        // left on screen. Titles are scrubbed again here in case a draft came
        // straight from the writer without being touched.
        if (state.mode === "auto") {
          fd.append("rows", JSON.stringify(state.drafts.map((d) => ({
            script: d.script.trim(), video_title: scrubTitle(d.video_title),
          }))));
          // Which history these rows belong to. Sent so the server can replace
          // the drafts it recorded when they were written with what is actually
          // being rendered — the edits are the version viewers see.
          fd.append("category", c.id);
          fd.append("language", els.lang.value);
        } else fd.append("excel", state.excel);
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
    let latest = null;
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
      if (s === "completed" && row.video_local_url) latest = row.video_local_url;
    });
    // Once, after the loop, rather than once per completed row: calling it
    // inside meant the monitor was rebuilt twelve times a tick and settled on
    // whichever row happened to be last.
    if (latest) showVideo(latest);
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
      U.toast("Render finished", { kind: "ok",
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
  const name = $("#catName"), mprompt = $("#catMotion");
  const brief = $("#catBrief");
  const errEl = $("#catErr"), dirtyEl = $("#catDirty");
  let editingId = null, snapshot = null;
  const fields = () => ({ label: name.value.trim(),
                          motion_prompt: mprompt.value.trim(), script_brief: brief.value.trim() });
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
    $("#catMotionN").textContent = mprompt.value.length || "";
    $("#catBriefN").textContent = brief.value.length || "";
    const seed = editingId ? seedById(editingId) : null;
    const cur = editingId ? cats.find((c) => c.id === editingId) : null;
    $("#catReset").hidden = !(seed && cur && FIELDS.some((k) => cur[k] !== seed[k]));
  }
  [name, mprompt, brief].forEach((el) => el.addEventListener("input", refreshEditor));

  function openEditor(id, from) {
    editingId = id;
    const c = id ? cats.find((x) => x.id === id) : from || null;
    $("#catTitle").textContent = id ? "Edit category" : "New category";
    $("#catSub").textContent = id ? c.label : from ? `Copy of ${from.label}` : "Motion prompt preset";
    name.value = c ? (id ? c.label : `${c.label} copy`) : "";
    mprompt.value = c ? c.motion_prompt || "" : "";
    brief.value = c ? c.script_brief || "" : "";
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
    persist(); renderCats(); refreshRun();
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
        name.value = seed.label;
        mprompt.value = seed.motion_prompt; brief.value = seed.script_brief || "";
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
      U.toast("Reattached to a render already in progress", { kind: "info",
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
  let savedMode = null;
  try { savedMode = localStorage.getItem(MODE); } catch (_) {}
  let savedSet = null;
  try { savedSet = localStorage.getItem(SET); } catch (_) {}
  applySet(savedSet === "single" ? "single" : "zodiac");
  restoreDrafts();
  applyMode(["auto", "single", "batch"].includes(savedMode) ? savedMode : "auto");
  renderDrafts();
  showPlaceholder();
  reattach();

  window.PaneVideo = { refreshRun, isRunning: () => state.running, fitDrafts: fitAll };
})();
