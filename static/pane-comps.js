/* Compilations pane — rhyme videos merged into one YouTube comp.
   The work is done by the Comp engine on the Mac mini (comp_gen/source), reached
   through /comps (api/routers/comps.py).

   Kept deliberately quiet. The screen holds three things: the rhymes, the comp
   being built (name, order, one button), and the comps already made, each with
   one action. Engine state (library mounted, intro found, ffmpeg) only appears
   as a banner when it is wrong; a finished comp's player, chapters, download
   and NAS path live together in one drawer rather than as four buttons a row.

   One draft per language — picks, order, name — kept in localStorage. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$, esc = U.esc;

  const DRAFT_KEY = "autodub_comps_draft", OPTS_KEY = "autodub_comps_opts", LANG_KEY = "autodub_comps_lang";
  const POLL_BUSY = 2000, POLL_IDLE = 15000, INFO_PARALLEL = 4, RECENT = 3;
  const PRESETS = {
    "1080": { resolution: "1920x1080", fps: "30" },
    "4k": { resolution: "3840x2160", fps: "30" },
    shorts: { resolution: "1080x1920", fps: "30" },
  };
  const ACTIVE = new Set(["queued", "running", "filing"]);
  const BADGE = {
    queued: ["", "Waiting"], running: ["a", "Making"], filing: ["a", "Saving"],
    filed: ["g", "Ready"], not_on_nas: ["o", "Not on NAS"], error: ["r", "Failed"], cancelled: ["", "Cancelled"],
  };

  const els = {
    pane: $("#pane-comps"), banners: $("#compsBanners"), live: $("#compsLive"),
    langs: $("#cLangs"), search: $("#cSearch"), grid: $("#cGrid"),
    name: $("#cName"), seq: $("#cSeq"), ioNote: $("#cIoNote"), reset: $("#cReset"),
    preset: $("#cPreset"), transition: $("#cTransition"), quality: $("#cQuality"), cut: $("#cCut"),
    run: $("#cRun"), msg: $("#cMsg"),
    recentWrap: $("#cRecentWrap"), recent: $("#cRecent"), showAll: $("#cShowAll"),
    drawer: $("#cDrawer"), scrim: $("#cScrim"), dTitle: $("#cDTitle"), dSub: $("#cDSub"), dVideo: $("#cDVideo"),
    dChapters: $("#cDChapters"), dCopy: $("#cDCopy"), dDownload: $("#cDDownload"), dPath: $("#cDPath"), dClose: $("#cDClose"),
  };

  // ── state ────────────────────────────────────────────────────────────────
  let cfg = null, items = [], langs = [], nextNames = {}, jobs = [], loaded = false, engineUp = true, showAll = false;
  let lang = load(LANG_KEY, null), query = "", lastClicked = null, openJob = null;
  const info = new Map(), det = new Map(); // ref -> engine /api/info, /api/detect
  const drafts = load(DRAFT_KEY, {});
  const opts = Object.assign({ preset: "1080", transition: "fade", quality: "balanced", cut: "auto" }, load(OPTS_KEY, {}));

  function load(key, dflt) { try { const v = JSON.parse(localStorage.getItem(key)); return v ?? dflt; } catch (_) { return dflt; } }
  function save(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (_) {} }
  const draft = () => (drafts[lang || ""] ||= { sel: [], name: "" });
  const saveDraft = () => save(DRAFT_KEY, drafts);
  const bare = (name) => String(name || "").replace(/\.mp4$/i, "");

  // ── talking to the engine ───────────────────────────────────────────────
  async function api(path, body) {
    const r = await U.fetchT("/comps/api/" + path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }, 60000);
    let p = {};
    try { p = await r.json(); } catch (_) {}
    if (!r.ok) throw Object.assign(new Error(p.error || `HTTP ${r.status}`), { status: r.status, down: p.engine === false });
    return p;
  }

  function engineDown(e) {
    engineUp = false;
    U.banner(els.banners, {
      kind: "error", id: "engine", title: "Compilations can't be made right now",
      detail: "The Comp engine on the Mac mini isn't answering. " + esc(e && e.down ? "" : (e ? e.message : "")),
      actions: [{ label: "Try again", onClick: () => boot() }],
    });
    refreshRun();
  }

  // ── time ─────────────────────────────────────────────────────────────────
  function hms(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  }
  function roughly(sec) {
    if (sec < 60) return "under a minute";
    const m = Math.round(sec / 60);
    return m < 60 ? `about ${m} min` : `about ${(sec / 3600).toFixed(1)} h`;
  }

  // What a rhyme contributes once a baked-in intro/outro is trimmed. null until known.
  // Mirrors the engine's cutFor: it never trims a rhyme down to under a second.
  function trims(rel) {
    const d = det.get("rhyme:" + rel), i = info.get("rhyme:" + rel);
    if (!d || !i || !i.duration || opts.cut === "off") return { start: 0, end: 0 };
    const all = opts.cut === "always";
    const start = (all ? d.intro_len > 0 : d.intro) ? d.intro_len : 0;
    const end = (all ? d.outro_len > 0 : d.outro) ? d.outro_len : 0;
    return i.duration - start - end < 1 ? { start: 0, end: 0 } : { start, end };
  }
  function rhymeLen(rel) {
    const i = info.get("rhyme:" + rel);
    if (!i || !i.duration) return null;
    const t = trims(rel);
    return i.duration - t.start - t.end;
  }

  // Probing a file is an ffmpeg call over SMB on the engine: a few at a time, each once.
  const infoWant = [], infoBusy = new Set();
  function wantInfo(ref, alsoDetect) {
    if (!info.has(ref) && !infoBusy.has(ref) && !infoWant.includes(ref)) infoWant.push(ref);
    const dk = "d|" + ref;
    if (alsoDetect && !det.has(ref) && !infoWant.includes(dk) && !infoBusy.has(dk)) infoWant.unshift(dk);
    pumpInfo();
  }
  function pumpInfo() {
    while (infoBusy.size < INFO_PARALLEL && infoWant.length) {
      const key = infoWant.shift();
      infoBusy.add(key);
      const isDet = key.startsWith("d|"), ref = isDet ? key.slice(2) : key;
      api(`${isDet ? "detect" : "info"}?ref=${encodeURIComponent(ref)}`)
        .then((p) => { (isDet ? det : info).set(ref, p); if (isDet && p.duration && !info.has(ref)) info.set(ref, { duration: p.duration }); })
        .catch(() => { if (!isDet) info.set(ref, {}); })
        .finally(() => { infoBusy.delete(key); paintDuration(ref); pumpInfo(); });
    }
  }

  // ── the rhymes ──────────────────────────────────────────────────────────
  async function loadLibrary() {
    els.grid.setAttribute("aria-busy", "true");
    try {
      const p = await api("rhymes");
      items = p.items || []; langs = p.langs || []; nextNames = p.next_names || {};
      U.clearBanners(els.banners, "warn");
    } catch (e) {
      if (e.status === 503) return engineDown(e);
      items = []; langs = [];
      U.banner(els.banners, { kind: "warn", id: "library", title: "The rhymes couldn't be listed",
        detail: esc(e.message), actions: [{ label: "Try again", onClick: () => loadLibrary() }] });
    } finally { els.grid.removeAttribute("aria-busy"); }
    if (!langs.some((l) => l.name === lang)) lang = langs.length ? langs[0].name : null;
    // drop picks whose file has gone from the library
    const have = new Set(items.map((i) => i.rel));
    Object.values(drafts).forEach((d) => { d.sel = (d.sel || []).filter((r) => have.has(r)); });
    loaded = true;
    renderLangs(); switchLang(lang);
  }

  function renderLangs() {
    els.langs.innerHTML = langs.map((l) =>
      `<button type="button" data-lang="${esc(l.name)}" aria-pressed="${l.name === lang}">${esc(l.name || "Other")}` +
      `<span class="n">${l.count}</span></button>`).join("");
    els.langs.hidden = langs.length < 2;
  }

  function switchLang(name) {
    lang = name; save(LANG_KEY, lang);
    els.langs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
    const d = draft();
    els.name.value = d.name || "";
    els.name.placeholder = nextNames[lang || ""] || "Name this comp";
    lastClicked = null;
    d.sel.forEach((rel) => wantInfo("rhyme:" + rel, true));
    renderGrid(); renderSeq();
  }

  const visibleItems = () => {
    const q = query.trim().toLowerCase();
    return items.filter((i) => (i.lang || "") === (lang || "") && (!q || i.name.toLowerCase().includes(q)));
  };

  let io = null;
  function renderGrid() {
    const list = visibleItems(), sel = draft().sel;
    if (!loaded) { els.grid.innerHTML = ""; return; }
    if (!list.length) {
      els.grid.innerHTML = `<div class="empty"><svg class="ri g" aria-hidden="true"><use href="/static/icons.svg?v=3#film"/></svg>` +
        `<div class="t">${items.length ? "No rhymes match" : "No rhymes yet"}</div>` +
        `<div class="s">${items.length ? "Try another search." : "Rhyme videos go in a language folder on the NAS share."}</div></div>`;
      return;
    }
    els.grid.innerHTML = list.map((it) => {
      const ref = "rhyme:" + it.rel, n = sel.indexOf(it.rel) + 1, len = rhymeLen(it.rel);
      return `<button type="button" class="rtile" data-rel="${esc(it.rel)}" aria-pressed="${n > 0}" title="${esc(it.name)}">` +
        `<span class="th"><img loading="lazy" decoding="async" alt="" src="/comps/thumb?ref=${encodeURIComponent(ref)}" />` +
        `<span class="ord">${n || ""}</span><span class="dur">${len ? hms(len) : ""}</span>` +
        (it.used ? `<span class="used">Used ${it.used}×</span>` : "") + `</span>` +
        `<span class="nm">${esc(it.name)}</span></button>`;
    }).join("");
    if (io) io.disconnect();
    io = new IntersectionObserver((ents) => ents.forEach((e) => {
      if (e.isIntersecting) { io.unobserve(e.target); wantInfo("rhyme:" + e.target.dataset.rel, false); }
    }), { root: els.pane, rootMargin: "200px" });
    els.grid.querySelectorAll(".rtile").forEach((t) => io.observe(t));
  }

  // Selection changes repaint attributes only, so thumbnails never reload.
  function paintSelection() {
    const sel = draft().sel;
    els.grid.querySelectorAll(".rtile").forEach((t) => {
      const n = sel.indexOf(t.dataset.rel) + 1;
      t.setAttribute("aria-pressed", String(n > 0));
      t.querySelector(".ord").textContent = n || "";
    });
  }
  function paintDuration(ref) {
    if (!ref.startsWith("rhyme:")) { renderSeq(); return; }
    const rel = ref.slice(6), t = els.grid.querySelector(`.rtile[data-rel="${CSS.escape(rel)}"] .dur`);
    const len = rhymeLen(rel);
    if (t) t.textContent = len ? hms(len) : "";
    if (draft().sel.includes(rel)) renderSeq();
  }

  function pick(rel) { const d = draft(); if (!d.sel.includes(rel)) { d.sel.push(rel); wantInfo("rhyme:" + rel, true); } }
  function toggle(rel, range) {
    const d = draft();
    if (range && lastClicked && lastClicked !== rel) {
      // shift-click: everything between the last pick and this one, in the order shown
      const list = visibleItems().map((i) => i.rel), a = list.indexOf(lastClicked), b = list.indexOf(rel);
      if (a >= 0 && b >= 0) (a < b ? list.slice(a + 1, b + 1) : list.slice(b, a).reverse()).forEach(pick);
    } else if (d.sel.includes(rel)) d.sel.splice(d.sel.indexOf(rel), 1);
    else pick(rel);
    lastClicked = rel;
    saveDraft(); paintSelection(); renderSeq();
  }

  // ── the comp being built ────────────────────────────────────────────────
  const nameOf = (rel) => { const it = items.find((i) => i.rel === rel); return it ? it.name : rel; };
  function renderSeq() {
    const sel = draft().sel;
    els.seq.innerHTML = sel.length ? sel.map((rel, i) => {
      const len = rhymeLen(rel), t = trims(rel), last = i === sel.length - 1;
      const note = [t.start && "intro trimmed", t.end && "outro trimmed"].filter(Boolean).join(", ");
      return `<div class="row sq" draggable="true" data-i="${i}">` +
        `<span class="i">${i + 1}</span>` +
        `<span class="tx"><span class="t">${esc(nameOf(rel))}</span></span>` +
        `<span class="len">${len ? hms(len) : ""}${note ? ` <span class="note">· ${note}</span>` : ""}</span>` +
        `<span class="mv"><button type="button" class="iconbtn" data-act="up" aria-label="Move up"${i ? "" : " disabled"}>${U.icon("up")}</button>` +
        `<button type="button" class="iconbtn" data-act="down" aria-label="Move down"${last ? " disabled" : ""}>${U.icon("down")}</button></span>` +
        `<button type="button" class="iconbtn" data-act="rm" aria-label="Remove ${esc(nameOf(rel))}">${U.icon("close")}</button></div>`;
    }).join("") : `<div class="cempty">Click rhymes on the left to add them. They play in the order you pick them; drag to reorder.</div>`;

    const s = cfg && cfg.status, iOK = s && s.intro_ok, oOK = s && s.outro_ok;
    els.ioNote.textContent = !s ? "" : iOK && oOK ? "Intro and outro are added automatically"
      : iOK ? "Intro is added automatically · no outro found" : oOK ? "Outro is added automatically · no intro found"
        : "No intro or outro found";
    els.reset.hidden = !sel.length;
    refreshRun();
  }

  function totalSeconds() {
    let t = 0, unknown = false;
    const add = (v) => { if (v == null) unknown = true; else t += v; };
    if (cfg && cfg.status.intro_ok) add(info.get("intro") && info.get("intro").duration);
    draft().sel.forEach((rel) => add(rhymeLen(rel)));
    if (cfg && cfg.status.outro_ok) add(info.get("outro") && info.get("outro").duration);
    return { t, unknown };
  }

  // The button says what the comp will be; the line under it says what's wrong, if anything.
  function refreshRun() {
    const sel = draft().sel, { t, unknown } = totalSeconds();
    let why = "", warn = "";
    if (!engineUp) why = "The Comp engine isn't answering.";
    else if (cfg && !cfg.status.share_ok) why = "The rhymes library isn't reachable.";
    else if (!sel.length) why = "";
    else {
      const short = sel.filter((rel) => { const l = rhymeLen(rel); return l != null && l < 10; });
      if (short.length) warn = `${short.length === 1 ? `“${nameOf(short[0])}” is` : `${short.length} rhymes are`} under 10 seconds — YouTube won't make a chapter that short.`;
    }
    els.run.disabled = Boolean(why) || !sel.length;
    els.run.textContent = sel.length
      ? `Create comp · ${sel.length} rhyme${sel.length === 1 ? "" : "s"}, ${unknown ? "~" : ""}${hms(t)}` : "Create comp";
    els.msg.textContent = why || warn;
    els.msg.classList.toggle("err", Boolean(why));
  }

  async function create() {
    if (els.run.disabled) return;
    const d = draft(), name = els.name.value.trim() || els.name.placeholder;
    const refs = [];
    if (cfg.status.intro_ok) refs.push("intro");
    d.sel.forEach((rel) => refs.push("rhyme:" + rel));
    if (cfg.status.outro_ok) refs.push("outro");
    const p = PRESETS[opts.preset] || PRESETS["1080"];
    els.run.disabled = true;
    try {
      await api("queue/add", { refs, options: {
        lang: lang || "", name, resolution: p.resolution, fps: p.fps, transition: opts.transition,
        fade_seconds: 0.5, quality: opts.quality, cut_inner: opts.cut,
      } });
      U.toast(`Making “${name}”`, { kind: "ok", detail: "It's saved to the NAS when it's done. You can start the next one now." });
      U.askNotify();
      d.sel = []; d.name = "";
      // the next suggestion, without waiting on a library re-list
      const next = name.replace(/(\d+)$/, (m) => String(Number(m) + 1));
      if (next !== name) nextNames[lang || ""] = next;
      saveDraft(); switchLang(lang);
      pollSoon(300);
    } catch (e) {
      if (e.status === 503) engineDown(e);
      else U.toast("Couldn't start the comp", { kind: "error", detail: esc(e.message) });
    } finally { refreshRun(); }
  }

  // ── comps already made ──────────────────────────────────────────────────
  const prevState = new Map();
  let pollTimer = null;
  function pollSoon(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(pollQueue, ms); }
  async function pollQueue() {
    try {
      jobs = (await api("queue")).jobs || [];
      if (!engineUp) { engineUp = true; U.clearBanners(els.banners, "error"); refreshRun(); }
    } catch (e) {
      if (e.status === 503) engineDown(e);
      pollSoon(POLL_IDLE); return;
    }
    let filed = false;
    jobs.forEach((j) => {
      const was = prevState.get(j.id);
      if (was && was !== j.state) {
        if (j.state === "filed") { U.notify("Comp ready", bare(j.name) + " is on the NAS"); filed = true; }
        else if (j.state === "not_on_nas" || j.state === "error") U.notify("Comp needs attention", bare(j.name), true);
      }
      prevState.set(j.id, j.state);
    });
    if (filed && loaded) loadLibrary(); // "used" counts and the next name moved
    renderRecent();
    const busy = jobs.some((j) => ACTIVE.has(j.state));
    els.live.hidden = !busy;
    if (window.Shell) window.Shell.setRunning("comps", busy, busy ? "Making comps" : "");
    pollSoon(busy ? POLL_BUSY : POLL_IDLE);
  }

  function jobLine(j) {
    switch (j.state) {
      case "queued": return "Waits for the one before it";
      case "running": {
        const left = j.speed > 0 && j.total && j.progress > 0.02 ? (1 - j.progress) * j.total / j.speed : null;
        return left != null ? `${roughly(left)} left` : "Starting…";
      }
      case "filing": return "Saving to the NAS…";
      case "filed": return `${j.clips} rhyme${j.clips === 1 ? "" : "s"} · ${hms(j.total)}`;
      case "not_on_nas": return "Made, but couldn't be saved to the NAS. " + (j.message || "");
      default: return j.message;
    }
  }

  function renderRecent() {
    // Newest first: everything still in progress, plus the last few finished.
    const all = jobs.slice().reverse();
    const shown = showAll ? all : all.filter((j, i) => ACTIVE.has(j.state) || j.state === "not_on_nas" || i < RECENT);
    els.recentWrap.hidden = !jobs.length;
    els.showAll.hidden = all.length <= shown.length && !showAll;
    els.showAll.textContent = showAll ? "Show fewer" : `Show all ${all.length}`;
    els.recent.innerHTML = shown.map((j) => {
      const [k, label] = BADGE[j.state] || ["", j.state];
      const pct = j.state === "filing" ? Math.round((j.file_progress || 0) * 100) : Math.round((j.progress || 0) * 100);
      const bar = j.state === "running" || j.state === "filing"
        ? `<div class="bar thin"><i class="${j.state === "filing" ? "up" : "done live"}" style="width:${pct}%"></i></div>` : "";
      let act = "";
      if (j.output && (j.state === "filed" || j.state === "not_on_nas")) act = `<button type="button" class="btn sm" data-act="open">Open</button>`;
      if (j.state === "not_on_nas") act += `<button type="button" class="btn sm p" data-act="refile">Save to NAS again</button>`;
      if (j.state === "queued" || j.state === "running") act = `<button type="button" class="btn q sm" data-act="cancel">Cancel</button>`;
      const bad = j.state === "error" || j.state === "not_on_nas";
      return `<div class="row cjob" data-id="${j.id}"><span class="tx">` +
        `<span class="t">${esc(bare(j.name))} <span class="badge ${k}">${label}</span></span>${bar}` +
        `<span class="s${bad ? " bad" : ""}">${esc(jobLine(j))}</span></span><span class="rt">${act}</span></div>`;
    }).join("");
  }
  els.showAll.addEventListener("click", () => { showAll = !showAll; renderRecent(); });

  els.recent.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const j = jobs.find((x) => String(x.id) === b.closest(".cjob").dataset.id); if (!j) return;
    const act = b.dataset.act;
    if (act === "open") openComp(j);
    else if (act === "cancel") U.confirm({ title: `Cancel “${bare(j.name)}”?`, confirmLabel: "Cancel comp", cancelLabel: "Keep it",
      message: j.state === "running" ? "It stops now and the work so far is thrown away." : "It won't be made.",
      onOk: () => api("queue/cancel", { id: j.id }).then(() => pollSoon(200)).catch((err) =>
        U.toast("Couldn't cancel", { kind: "error", detail: esc(err.message) })) });
    else if (act === "refile") {
      b.disabled = true;
      try { await api("queue/refile", { id: j.id }); }
      catch (err) { U.toast("Couldn't save it", { kind: "error", detail: esc(err.message) }); }
      pollSoon(200);
    }
  });

  // ── a finished comp: one drawer for everything you do with it ───────────
  // "/Volumes/RHYMES/Comps Outs/Hindi/x.mp4" -> "RHYMES/Comps Outs/Hindi/x.mp4": what someone
  // browsing the share from their own machine sees.
  const sharePath = (p) => (p || "").replace(/^\/Volumes\//, "");
  const ov = U.overlay(els.drawer, els.scrim, { beforeClose: () => { els.dVideo.pause(); } });
  function openComp(j) {
    openJob = j;
    els.dTitle.textContent = bare(j.name);
    els.dSub.textContent = j.state === "filed" ? `On the NAS · ${sharePath(j.output_path)}` : "Not on the NAS yet";
    els.dVideo.src = `/comps/video?ref=${encodeURIComponent(j.output)}`;
    els.dChapters.value = j.chapters || "";
    els.dChapters.rows = Math.min(12, Math.max(3, (j.chapters || "").split("\n").length));
    els.dDownload.href = `/comps/video?ref=${encodeURIComponent(j.output)}&download=1`;
    els.dPath.hidden = j.state !== "filed";
    ov.open();
  }
  async function copy(text, done) {
    try { await navigator.clipboard.writeText(text); U.toast(done, { kind: "ok" }); }
    catch (_) { U.toast("Couldn't copy — select it and copy by hand", { kind: "warn", detail: `<code>${esc(text)}</code>` }); }
  }
  els.dCopy.addEventListener("click", () => copy(els.dChapters.value, "Chapters copied"));
  els.dPath.addEventListener("click", () => openJob && copy(sharePath(openJob.output_path), "NAS path copied"));
  els.dClose.addEventListener("click", () => ov.close());

  // ── wiring ───────────────────────────────────────────────────────────────
  els.langs.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) switchLang(b.dataset.lang); });
  els.search.addEventListener("input", () => { query = els.search.value; renderGrid(); });
  els.grid.addEventListener("click", (e) => { const t = e.target.closest(".rtile"); if (t) toggle(t.dataset.rel, e.shiftKey); });
  els.grid.addEventListener("error", (e) => { if (e.target.tagName === "IMG") e.target.classList.add("gone"); }, true);
  els.reset.addEventListener("click", () => { draft().sel = []; saveDraft(); paintSelection(); renderSeq(); });
  els.name.addEventListener("input", () => { draft().name = els.name.value; saveDraft(); });
  els.run.addEventListener("click", create);
  window.addEventListener("keydown", (e) => {
    if (els.pane.hidden || e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
    e.preventDefault(); create();
  });

  // reorder and remove: the arrows show on hover/focus, dragging works anywhere on the row
  els.seq.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const sel = draft().sel, i = Number(b.closest(".row").dataset.i), act = b.dataset.act;
    let focusAt = i;
    if (act === "rm") sel.splice(i, 1);
    else { const k = i + (act === "up" ? -1 : 1); if (k < 0 || k >= sel.length) return; [sel[i], sel[k]] = [sel[k], sel[i]]; focusAt = k; }
    saveDraft(); paintSelection(); renderSeq();
    const again = els.seq.querySelector(`.row[data-i="${focusAt}"] [data-act="${act}"]`);
    if (again && !again.disabled) again.focus();
  });
  let dragI = null;
  els.seq.addEventListener("dragstart", (e) => {
    const r = e.target.closest(".row[data-i]"); if (!r) return;
    dragI = Number(r.dataset.i); r.classList.add("dragging"); e.dataTransfer.effectAllowed = "move";
  });
  els.seq.addEventListener("dragover", (e) => {
    const r = e.target.closest(".row[data-i]"); if (!r || dragI === null) return;
    e.preventDefault();
    els.seq.querySelectorAll(".over").forEach((x) => x.classList.remove("over"));
    r.classList.add("over");
  });
  els.seq.addEventListener("drop", (e) => {
    const r = e.target.closest(".row[data-i]"); if (!r || dragI === null) return;
    e.preventDefault();
    const sel = draft().sel, [m] = sel.splice(dragI, 1);
    sel.splice(Number(r.dataset.i), 0, m);
    dragI = null; saveDraft(); paintSelection(); renderSeq();
  });
  els.seq.addEventListener("dragend", () => { dragI = null; renderSeq(); });

  // format, then the rest behind "More options"
  els.preset.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.preset === opts.preset)));
  U.bindSeg(els.preset, (ds) => { opts.preset = ds.preset; save(OPTS_KEY, opts); });
  ["transition", "quality", "cut"].forEach((key) => {
    els[key].value = opts[key];
    els[key].addEventListener("change", () => {
      opts[key] = els[key].value; save(OPTS_KEY, opts);
      if (key === "cut") { renderGrid(); renderSeq(); }
    });
  });

  // ── problems, as banners — and nothing when all is well ─────────────────
  function renderProblems() {
    const s = cfg.status;
    if (!s.ffmpeg) U.banner(els.banners, { kind: "error", id: "ffmpeg", title: "ffmpeg is missing on the Mac mini",
      detail: "Install it there with <code>brew install ffmpeg</code>, then restart the Comp engine." });
    else if (!s.share_ok) U.banner(els.banners, { kind: "warn", id: "share", title: "The rhymes library isn't reachable",
      detail: "Check the NAS share is mounted on the Mac mini, then try again.",
      actions: [{ label: "Try again", onClick: () => boot() }] });
  }

  async function boot() {
    U.clearBanners(els.banners);
    try { cfg = await api("config"); engineUp = true; }
    catch (e) { return engineDown(e); }
    renderProblems();
    if (cfg.status.intro_ok) wantInfo("intro");
    if (cfg.status.outro_ok) wantInfo("outro");
    await loadLibrary();
    pollQueue();
  }

  window.PaneComps = {
    // Every visit re-reads the library, so new rhymes show up without a refresh button.
    activate() { if (!cfg) boot(); else loadLibrary(); },
  };
  // keep the sidebar's live dot honest even before the pane is opened
  pollQueue();
})();
