/* Story pane — a pasted story, broken into voiceover lines for review, then
   voiced. Paste → Review → Generate. Nothing before Generate spends
   text-to-speech credits. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  const els = {
    banners: $("#storyBanners"), paste: $("#sPaste"), review: $("#sReview"),
    title: $("#sTitle"), activity: $("#sActivity"), text: $("#sText"),
    langs: $("#sLangs"), selN: $("#sSelN"), groups: $("#sGroups"),
    split: $("#sSplit"), msg: $("#sMsg"), live: $("#storyLive"),
    lines: $("#sLines"), lineN: $("#sLineN"), reviewActivity: $("#sReviewActivity"),
    back: $("#sBack"), download: $("#sDownload"), reviewMsg: $("#sReviewMsg"),
    generate: $("#sGenerate"), cancel: $("#sCancel"),
    // the progress block, shaped as U.jobProgress expects
    progress: $("#storyProgress"), clips: $("#spClips"), detail: $("#spDetail"), started: $("#spStarted"),
    up: $("#spUp"), done: $("#spDone"), bad: $("#spBad"),
    upN: $("#spUpN"), doneN: $("#spDoneN"), badN: $("#spBadN"), queueN: $("#spQueueN"),
  };

  // emotions and maxLineChars come from the split response, so the dropdown and
  // the server's validator share one list
  const state = { splitting: false, activityEdited: false, lines: [], emotions: [], maxLineChars: 0,
                  running: false, jobId: null, targets: [] };
  const JOB_KEY = "autodub_story_job";

  const SEL = U.langPicker({ tokens: els.langs, groups: els.groups, count: els.selN,
                             onChange: () => { if (!els.review.hidden) summarise(); } });

  // ── paste ────────────────────────────────────────────────────────────────
  // The same rule the server applies; shown live so the operator can see and
  // change it before anything is sent.
  const activityFrom = (title) => title.toLowerCase().replace(/[^a-z0-9]/g, "") + "story";
  els.title.addEventListener("input", () => {
    if (!state.activityEdited) els.activity.value = activityFrom(els.title.value);
    refreshRun();
  });
  els.activity.addEventListener("input", () => {
    // clearing the field hands it back to the title
    state.activityEdited = els.activity.value.trim() !== "";
    if (!state.activityEdited) els.activity.value = activityFrom(els.title.value);
    refreshRun();
  });
  els.text.addEventListener("input", refreshRun);

  function blocker() {
    if (!els.title.value.trim()) return "Give the story a title";
    if (!els.text.value.trim()) return "Paste a story to start";
    return null;
  }
  function refreshRun() {
    if (state.splitting) { els.split.disabled = true; return; }
    const why = blocker();
    els.split.disabled = Boolean(why);
    els.msg.textContent = why || "";
  }
  const say = (text) => { els.msg.textContent = text || ""; };

  // ── break into lines ─────────────────────────────────────────────────────
  els.split.addEventListener("click", async () => {
    if (blocker() || state.splitting) return;
    state.splitting = true;
    U.clearBanners(els.banners);
    say("Gemini is reading the story…");
    els.split.innerHTML = `<span class="spin"></span>Breaking into lines…`;
    els.live.hidden = false;
    window.Shell.setRunning("story", true, "Story splitting");
    refreshRun();
    try {
      // Up to two Gemini calls when the first split is rejected, so this waits
      // longer than a normal request.
      const r = await U.fetchT("/batch/story/split", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: els.title.value, text: els.text.value }),
      }, 240000);
      if (!r.ok) {
        let detail = "";
        try { detail = (await r.json()).detail; } catch (_) { detail = `HTTP ${r.status}`; }
        // FastAPI's own 422s carry a list of problems, not a sentence
        if (typeof detail !== "string") detail = JSON.stringify(detail);
        throw Object.assign(new Error(detail || `HTTP ${r.status}`), { status: r.status });
      }
      const d = await r.json();
      state.lines = d.lines.map((l) => ({ text: l.text, emotion: l.emotion }));
      state.emotions = d.emotions || [];
      state.maxLineChars = d.max_line_chars || 0;
      if (!state.activityEdited) els.activity.value = d.activity_name;
      say("");
      showReview();
    } catch (e) {
      const raw = e.name === "AbortError" ? "Gemini took too long to answer." : String(e.message || e);
      // a 400 is about the story itself; anything else is the service
      U.banner(els.banners, {
        kind: "error", id: "story-split",
        title: e.status === 400 ? "The story could not be broken into lines" : "Gemini could not be reached",
        detail: e.status === 400 ? U.esc(raw) : "Nothing was spent. Try again in a minute.",
        raw: e.status === 400 ? undefined : raw,
      });
    } finally {
      state.splitting = false;
      els.split.textContent = "Break into lines";
      els.live.hidden = true;
      window.Shell.setRunning("story", false);
      refreshRun();
    }
  });

  // ── review ───────────────────────────────────────────────────────────────
  function showReview() {
    els.paste.hidden = true;
    els.review.hidden = false;
    els.reviewActivity.textContent = els.activity.value.trim();
    renderLines();
  }
  els.back.addEventListener("click", () => {
    els.review.hidden = true;
    els.paste.hidden = false;
    els.text.focus();
  });

  const tagged = (l) => `[${l.emotion}] ${l.text}`.length;
  const fit = (area) => { area.style.height = "auto"; area.style.height = area.scrollHeight + "px"; };

  function renderLines() {
    els.lines.innerHTML = "";
    els.lineN.textContent = state.lines.length;
    const options = state.emotions.map((e) => `<option value="${U.esc(e)}">${U.esc(e)}</option>`).join("");
    state.lines.forEach((line, i) => {
      const row = document.createElement("div");
      row.className = "qrow";
      row.innerHTML =
        `<span class="i">${i + 1}</span>` +
        `<textarea class="area" rows="1" aria-label="Line ${i + 1} text"></textarea>` +
        `<select class="fld" aria-label="Line ${i + 1} emotion">${options}</select>` +
        `<span class="tools"><button type="button" class="btn" data-act="split" title="Split this line at the cursor">Split</button>` +
        `<button type="button" class="btn" data-act="merge" title="Merge with the next line"${i === state.lines.length - 1 ? " disabled" : ""}>Merge</button></span>`;
      const area = row.querySelector("textarea"), pick = row.querySelector("select");
      area.value = line.text;
      pick.value = line.emotion;
      area.addEventListener("input", () => { line.text = area.value; fit(area); flag(row, line); });
      pick.addEventListener("change", () => { line.emotion = pick.value; flag(row, line); });
      row.querySelector('[data-act="split"]').addEventListener("click", () => splitAt(i, area.selectionStart));
      row.querySelector('[data-act="merge"]').addEventListener("click", () => mergeNext(i));
      els.lines.appendChild(row);
      fit(area);
      flag(row, line, false);
    });
    summarise();
  }

  // Merge joins with a space and keeps the first line's emotion.
  function mergeNext(i) {
    const a = state.lines[i], b = state.lines[i + 1];
    if (!b) return;
    const merged = { text: `${a.text.trim()} ${b.text.trim()}`.trim(), emotion: a.emotion };
    if (state.maxLineChars && tagged(merged) > state.maxLineChars) {
      els.reviewMsg.textContent = `Lines ${i + 1} and ${i + 2} are too long to merge: over ${state.maxLineChars} characters`;
      els.reviewMsg.classList.add("err");
      return;
    }
    state.lines.splice(i, 2, merged);
    renderLines();
  }
  // Split at the cursor; both halves keep the emotion.
  function splitAt(i, pos) {
    const line = state.lines[i];
    const head = line.text.slice(0, pos).trim(), tail = line.text.slice(pos).trim();
    if (!head || !tail) {
      els.reviewMsg.textContent = "Put the cursor inside the line, between the words to split";
      els.reviewMsg.classList.add("err");
      return;
    }
    state.lines.splice(i, 1, { text: head, emotion: line.emotion }, { text: tail, emotion: line.emotion });
    renderLines();
  }

  els.download.addEventListener("click", async () => {
    const bad = state.lines.some((l) => !l.text.trim());
    if (!state.lines.length || bad) { summarise(); return; }
    try {
      const r = await U.fetchT("/batch/story/xlsx", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ activity_name: els.reviewActivity.textContent, lines: state.lines }),
      }, 30000);
      if (!r.ok) {
        let d = ""; try { d = (await r.json()).detail; } catch (_) {}
        throw new Error(typeof d === "string" && d ? d : `HTTP ${r.status}`);
      }
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url; a.download = `${els.reviewActivity.textContent}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      els.reviewMsg.textContent = `Download failed: ${e.message}`;
      els.reviewMsg.classList.add("err");
    }
  });

  // A line too long to voice, or emptied, is marked where it is.
  function flag(row, line, resummarise = true) {
    const bad = !line.text.trim() || (state.maxLineChars && tagged(line) > state.maxLineChars);
    row.dataset.flag = bad ? "bad" : "";
    if (resummarise) summarise();
  }
  function summarise() {
    const empty = state.lines.filter((l) => !l.text.trim()).length;
    const long = state.maxLineChars ? state.lines.filter((l) => tagged(l) > state.maxLineChars).length : 0;
    const problems = [];
    if (empty) problems.push(`${empty} empty line${empty === 1 ? "" : "s"}`);
    if (long) problems.push(`${long} line${long === 1 ? "" : "s"} over ${state.maxLineChars} characters`);
    if (!state.lines.length) problems.push("no lines");
    if (!SEL.size) problems.push("pick at least one target language on the story step");
    if (!window.Shell.configured()) problems.push("runtime config is not ready");
    if (!state.running) {
      els.reviewMsg.textContent = problems.length
        ? problems.join(" · ")
        : `${state.lines.length} lines × ${SEL.size} language${SEL.size === 1 ? "" : "s"} = ` +
          `${state.lines.length * SEL.size} clips`;
      els.reviewMsg.classList.toggle("err", problems.length > 0);
    }
    refreshGenerate(problems.length > 0);
  }

  // ── generate ─────────────────────────────────────────────────────────────
  function refreshGenerate(blocked) {
    els.cancel.hidden = !state.running;
    els.generate.disabled = state.running || state.splitting || blocked;
    els.back.disabled = state.running;
  }

  els.generate.addEventListener("click", generate);
  U.bindJobCancel(els.cancel, () => state.jobId);

  async function generate() {
    if (state.running) return;
    U.clearBanners(els.banners);
    // Not awaited, as in Audio: the run must not wait on a permission prompt.
    U.askNotify();
    state.running = true; state.targets = [...SEL];
    els.live.hidden = false;
    els.reviewMsg.textContent = "Starting…"; els.reviewMsg.classList.remove("err");
    window.Shell.setRunning("story", true, "Story audio running");
    summarise();

    try {
      const r = await U.fetchT("/batch/story-jobs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: els.title.value.trim(),
          activity_name: els.reviewActivity.textContent,
          lines: state.lines,
          target_languages: state.targets,
        }),
      }, 30000);
      if (!r.ok) {
        let detail = "";
        try { detail = (await r.json()).detail; } catch (_) {}
        if (typeof detail !== "string" || !detail) detail = `HTTP ${r.status}`;
        throw Object.assign(new Error(detail), { status: r.status });
      }
      const { job_id } = await r.json();
      state.jobId = job_id;
      try { localStorage.setItem(JOB_KEY, job_id); } catch (_) {}
      await poll(job_id);
    } catch (e) {
      // a 400 is about the lines or the setup; anything else never got there
      U.banner(els.banners, e.status === 400
        ? { kind: "error", title: "The story could not be voiced", detail: U.esc(e.message) }
        : { kind: "error", title: "Could not start the run",
            detail: "The request never reached the server.", raw: String(e && e.message || e) });
      finish("failed", null, null, false);
    }
  }

  async function poll(id) {
    const end = await U.pollJob(id, (summary, status) => {
      U.jobProgress(els, summary, status, state.targets.length || SEL.size);
      window.Shell.setRunning("story", true, shortProgress(summary));
    });
    finish(end.status, end.summary, end.cause);
  }
  function shortProgress(s) {
    s = s || {};
    return s.total_rows ? `Story ${s.rows_processed || 0}/${s.total_rows}` : "Story audio running";
  }

  function finish(status, summary, cause, report = true) {
    state.running = false; state.jobId = null;
    try { localStorage.removeItem(JOB_KEY); } catch (_) {}
    els.live.hidden = true;
    U.resetJobCancel(els.cancel);
    window.Shell.setRunning("story", false);
    if (summary) U.jobProgress(els, summary, status, state.targets.length || SEL.size);
    summarise();
    if (!report) return;
    U.jobEnded({ banners: els.banners, status, summary, cause, name: "Story audio" });
    storyFixReport(summary);
  }

  // Lines the story fix could not cover got the line-by-line fix instead, and
  // lines whose length moved far from the draft are worth a listen.
  function storyFixReport(s) {
    s = s || {};
    const fell = s.story_fix_fallback_lines || 0, flagged = s.story_fix_flagged_lines || 0;
    if (!fell && !flagged && !s.story_fix_warning) return;
    const counts = [
      fell && `${fell} line${fell === 1 ? "" : "s"} used the line-by-line fix`,
      flagged && `${flagged} line${flagged === 1 ? "" : "s"} changed length a lot — worth a listen`,
    ].filter(Boolean).join(" · ");
    U.banner(els.banners, { kind: "warn", id: "story-fix", title: "Some lines were fixed differently",
      detail: [counts, s.story_fix_warning, "Listen to these lines in the ZIP before publishing."]
        .filter(Boolean).map(U.esc).join("<br>") });
  }

  // ── survive a reload ─────────────────────────────────────────────────────
  // The reviewed lines are gone after a reload, but the run is not: progress,
  // results and cancel come back.
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
      els.live.hidden = false;
      // the cancel button lives on the review step
      showReview();
      els.reviewMsg.textContent = "Running…"; els.reviewMsg.classList.remove("err");
      window.Shell.setRunning("story", true, "Story audio running");
      U.toast("Reattached to a story run already in progress", { kind: "info",
        detail: "Nothing was lost when the page reloaded." });
      poll(id);
    } catch (_) {}
  }

  // The line boxes size themselves to their text, which they cannot measure
  // while the pane is hidden.
  function fitLines() { els.lines.querySelectorAll("textarea").forEach(fit); }

  refreshRun(); reattach();
  window.PaneStory = { refreshRun, fitLines, isSplitting: () => state.splitting,
                       isRunning: () => state.running };
})();
