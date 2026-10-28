/* Video pane — Auto mode: scripts written from the category's brief, held as
   drafts to read and edit before anything renders. */
(() => {
  "use strict";
  const U = window.UI, V = window.Video;
  const { els, state, MAX_SCRIPT, NATIVE_LANGS, DRAFT_KEEP_DAYS } = V;
  const { LANG, SET, DRAFTS } = V.keys;

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
    refreshGen(); V.refreshRun();
  }
  els.autoTitle.addEventListener("input", () => V.refreshRun());

  // The button names its consequence: twelve paid renders is worth reading before
  // pressing, and "Generate batch" never said how many.
  function refreshRunLabel() {
    if (state.mode === "single") { U.label(els.run, "Generate video", "play"); return; }
    const n = state.mode === "auto" ? state.drafts.length : 0;
    U.label(els.run, n ? `Render ${n} video${n === 1 ? "" : "s"}` : "Generate batch", "play");
  }

  function refreshGen() {
    const c = V.activeCat();
    const brief = c && (c.script_brief || "").trim();
    const zodiac = state.set === "zodiac";
    els.gen.disabled = state.writing || state.running || !brief
      || (!zodiac && !els.autoTitle.value.trim());
    U.label(els.gen, state.writing ? "Writing…" : state.drafts.length ? "Rewrite scripts" : "Write scripts",
      "sparkle");
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
        language: els.lang.value, category: (V.activeCat() || {}).id || "",
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
    if (saved.category && V.cats.some((c) => c.id === saved.category)) {
      V.activeId = saved.category; V.persist(); V.renderCats();
    }
    if (saved.publish_date) els.date.value = saved.publish_date;

    if (saved.publish_date !== today()) {
      U.banner(els.banners, { kind: "info", title: `Restored ${state.drafts.length} drafts from an earlier day`,
        detail: `They were written for <code>${U.esc(saved.publish_date)}</code>, and the NAS folder date has been set to match. Rewrite them if you meant today.` });
    }
  }

  els.gen.addEventListener("click", write);
  async function write() {
    const c = V.activeCat();
    if (!c || !(c.script_brief || "").trim() || state.writing || state.running) return;
    if (state.set === "single" && !els.autoTitle.value.trim()) return;
    if (!V.dateSet()) return;   // scripts are filed by date; no silent default
    if (state.drafts.length) {
      const edited = state.drafts.some((d) => d.edited);
      if (edited) {
        U.confirm({ title: "Replace the drafts you edited?",
          message: "Rewriting throws away every script on screen, including your edits.",
          confirmLabel: "Rewrite", onOk: () => { state.drafts = []; write(); } });
        return;
      }
    }

    state.writing = true; refreshGen(); V.say(`Writing the scripts for ${els.date.value}…`);
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
      const clashes = Array.isArray(body.warnings) ? body.warnings : [];
      if (clashes.length) {
        // banner() takes detail as HTML, so each warning is escaped.
        U.banner(els.banners, { kind: "warn", id: "script-warnings",
          title: "Some drafts still clash",
          detail: clashes.map((w) => U.esc(String(w))).join("<br>") });
        V.say(`${state.drafts.length} scripts written, some still clash — read them before you run`);
      } else {
        V.say(`${state.drafts.length} scripts written — read them before you run`);
      }
    } catch (e) {
      const why = String(e && e.message || e);
      U.banner(els.banners, { kind: "error", title: "The scripts were not written",
        detail: "Nothing was rendered and nothing was charged.", raw: why });
      // Said on the message line too, in error style so refreshRun leaves it
      // alone. Cleared, the line fell back to the idle hint, and a failed
      // write read as a write that had never been asked for.
      V.say(`The scripts were not written: ${why}`, true);
    } finally {
      state.writing = false; renderDrafts(); refreshGen(); V.refreshRun();
    }
  }

  // A title becomes the NAS filename, so the same scrubbing the single-mode
  // title gets applies here — and it is applied to what is STORED, so what the
  // operator reads is what gets filed. The server scrubs single-video titles
  // with the same rule in services/video_pipeline/intake.py.
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
      els.drafts.innerHTML = `<div class="empty">${U.icon("edit", "g")}
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
          <span class="i">${i + 1}</span>
          <input type="text" class="fld" maxlength="80" value="${U.esc(draft.video_title)}"
            aria-label="File name for draft ${i + 1}" />
          <span class="ext">.mp4</span>
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
        draft.video_title = scrubTitle(title.value); draft.edited = true; saveDrafts(); V.refreshRun();
      });
      area.addEventListener("input", () => {
        draft.script = area.value; draft.edited = true; recount(); fit(); saveDrafts(); V.refreshRun();
      });
      recount();
      els.drafts.appendChild(el);
      fit();   // after insertion — scrollHeight is 0 while the node is detached
    });
  }

  Object.assign(V, { applySet, refreshRunLabel, refreshGen, restoreDrafts, renderDrafts, scrubTitle, fitAll });
})();
